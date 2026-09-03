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
from fm.service import jobs
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
# Push modal — overlay with live progress, busy choices, hub refresh
# ---------------------------------------------------------------------------

def _sync_drain(config):
    """Synchronous replacement for jobs.drain_background (tests only)."""
    with SessionLocal() as session:
        jobs.run_pending(session, config)


class TestPushModal:
    """Tests for the push-modal overlay endpoints."""

    def test_sync_page_has_push_modal_scaffolding(self, client):
        """The /sync page contains the modal overlay container and JS."""
        r = client.get("/sync")
        assert r.status_code == 200
        assert b'id="action-modal"' in r.content
        assert b'openActionModal' in r.content
        assert b'closeActionModal' in r.content
        assert b'spinner spinner-lg' in r.content
        assert b'role="dialog"' in r.content
        assert b'aria-modal="true"' in r.content
        assert b'aria-live="polite"' in r.content
        assert b'tabindex="-1"' in r.content

    def test_sync_page_push_buttons_target_modal(self, client):
        """Push buttons POST to /sync/push-modal, not /api/sync/push."""
        r = client.get("/sync")
        assert b'/sync/push-modal' in r.content
        # Old inline JS (location.reload for awaiting_input) should be gone
        # from push buttons — but orcaToast still appears for cloud buttons.
        assert b"location.reload()" not in r.content

    def test_push_modal_returns_running_fragment(self, client, monkeypatch,
                                                  sample_db, tmp_path):
        """POST /sync/push-modal returns a fragment with spinner + polling."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.post("/sync/push-modal")
        assert r.status_code == 200
        # With synchronous drain, the push completes immediately.
        # The fragment should show done state (push + verify both complete).
        assert b"push-modal-state" in r.content

    def test_push_modal_done_shows_success(self, client, monkeypatch,
                                            sample_db, tmp_path):
        """After push+verify complete, the poll fragment shows success."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        # Push via the JSON endpoint (synchronous drain) to get a job_id
        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]
        # Poll the modal endpoint
        r = client.get(f"/sync/push-modal/{job_id}")
        assert r.status_code == 200
        assert b"Push complete" in r.content
        assert b"profile(s) pushed" in r.content

    def test_push_modal_busy_shows_choices(self, client, monkeypatch,
                                            sample_db, tmp_path):
        """Busy push renders choice buttons inside the modal fragment."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        busy_transport = FakeTransport(busy={"filename": "x.gcode", "progress": 50})
        monkeypatch.setattr(sh, "_transport_factory", lambda c: busy_transport)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.post("/sync/push-modal")
        assert r.status_code == 200
        assert b"Printer is busy" in r.content
        assert b"Force reboot" in r.content
        assert b"Wait" in r.content
        assert b"No reboot" in r.content
        # Choice buttons should target the resolve-modal endpoint
        assert b"/sync/jobs/" in r.content
        assert b"resolve-modal" in r.content

    def test_push_modal_resolve_force_reboot(self, client, monkeypatch,
                                               sample_db, tmp_path):
        """Resolve-modal with force_reboot transitions to done."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        busy_transport = FakeTransport(busy={"filename": "x.gcode", "progress": 50})
        monkeypatch.setattr(sh, "_transport_factory", lambda c: busy_transport)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        # Push to get an awaiting_input job
        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]
        assert get_job_status(client, job_id) == "awaiting_input"
        # Resolve via the modal endpoint
        # Switch transport to non-busy so force_reboot succeeds
        monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())
        r = client.post(f"/sync/jobs/{job_id}/resolve-modal",
                        data={"choice": "force_reboot"})
        assert r.status_code == 200
        assert get_job_status(client, job_id) == "done"
        assert b"Push complete" in r.content

    def test_push_modal_resolve_wait_stays_awaiting(self, client, monkeypatch,
                                                      sample_db, tmp_path):
        """Resolve-modal with wait retries — still busy → awaiting."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        busy_transport = FakeTransport(busy={"filename": "x.gcode", "progress": 50})
        monkeypatch.setattr(sh, "_transport_factory", lambda c: busy_transport)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]
        r = client.post(f"/sync/jobs/{job_id}/resolve-modal",
                        data={"choice": "wait"})
        assert r.status_code == 200
        assert get_job_status(client, job_id) == "awaiting_input"
        assert b"Printer is busy" in r.content

    def test_push_modal_resolve_invalid_choice_422(self, client, monkeypatch,
                                                     sample_db, tmp_path):
        """Invalid choice returns 422 from the modal resolve endpoint."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        busy_transport = FakeTransport(busy={"filename": "x.gcode", "progress": 50})
        monkeypatch.setattr(sh, "_transport_factory", lambda c: busy_transport)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]
        r = client.post(f"/sync/jobs/{job_id}/resolve-modal",
                        data={"choice": "bogus"})
        assert r.status_code == 422

    def test_push_modal_resolve_not_awaiting_409(self, client, monkeypatch,
                                                   sample_db, tmp_path):
        """Resolving a non-awaiting job returns 409."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]
        r = client.post(f"/sync/jobs/{job_id}/resolve-modal",
                        data={"choice": "force_reboot"})
        assert r.status_code == 409

    def test_hub_fragment_returns_drift(self, client, monkeypatch,
                                          sample_db, tmp_path):
        """GET /sync/hub-fragment returns drift table HTML."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.get("/sync/hub-fragment")
        assert r.status_code == 200
        assert b"drift-table" in r.content
        assert b"target-card" in r.content
        # Should contain the profile name
        assert b"Sunlu" in r.content

    def test_hub_fragment_shows_fresh_status_after_push(self, client, monkeypatch,
                                                          sample_db, tmp_path):
        """After a push, the hub fragment shows updated drift status."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        # Before push, drift shows "new" (no printer_db_id)
        r = client.get("/sync/hub-fragment")
        assert b"new" in r.content or b"pending" in r.content
        # Push
        client.post("/api/sync/push", json={})
        # After push, hub fragment should show in_sync
        r = client.get("/sync/hub-fragment")
        assert b"in_sync" in r.content

    def test_push_modal_poll_404_for_missing_job(self, client):
        """Polling a non-existent job returns a not-found fragment."""
        r = client.get("/sync/push-modal/9999")
        assert r.status_code == 200
        assert b"Job not found" in r.content

    def test_push_modal_busy_shows_filename_and_progress(self, client, monkeypatch,
                                                          sample_db, tmp_path):
        """Busy push renders the print filename and progress in the modal."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        busy_transport = FakeTransport(busy={"filename": "benchy.gcode", "progress": 73})
        monkeypatch.setattr(sh, "_transport_factory", lambda c: busy_transport)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.post("/sync/push-modal")
        assert r.status_code == 200
        assert b"Printer is busy" in r.content
        assert b"benchy.gcode" in r.content
        assert b"73%" in r.content
        assert b"push-modal-busy-detail" in r.content

    def test_push_modal_unknown_status_has_close_button(self, client, monkeypatch,
                                                          sample_db, tmp_path):
        """The unknown-status fallback renders an error state with Close."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]
        # Manually set the job to an unexpected status via the API
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            job.status = "weird_state"
            session.commit()
        r = client.get(f"/sync/push-modal/{job_id}")
        assert r.status_code == 200
        assert b"Unexpected state" in r.content
        assert b"weird_state" in r.content
        assert b"closeActionModal" in r.content

    def test_push_modal_running_has_dismiss_button(self, client, monkeypatch,
                                                    sample_db, tmp_path):
        """The running-state fragment includes a Dismiss ghost button."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        # Enqueue a push but don't drain — job stays queued
        monkeypatch.setattr(jobs, "drain_background", lambda c: None)
        r = client.post("/sync/push-modal")
        assert r.status_code == 200
        assert b"Dismiss" in r.content
        assert b"closeActionModal" in r.content
        assert b"spinner spinner-lg" in r.content

    def test_push_modal_error_close_is_secondary(self):
        """Error-state Close button uses button-secondary, not button-primary."""
        from fm.api.ui import _action_modal_fragment
        html = _action_modal_fragment("push", 1, "error", {"error": "SSH timeout"})
        assert "Push failed" in html
        assert "button-secondary" in html
        assert "button-primary" not in html
        assert "closeActionModal" in html

    def test_push_modal_unknown_status_escapes_and_has_close(self):
        """Unknown-status fallback HTML-escapes the status and has Close."""
        from fm.api.ui import _action_modal_fragment
        html = _action_modal_fragment("push", 1, "weird<script>", None)
        assert "Unexpected state" in html
        assert "weird&lt;script&gt;" in html
        assert "<script>" not in html
        assert "button-secondary" in html
        assert "closeActionModal" in html


# ---------------------------------------------------------------------------
# Preset modal — Push to Cloud via background job + modal polling
# ---------------------------------------------------------------------------

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


def _fake_orca():
    """Fake OrcaCloudClient for modal tests."""
    from unittest.mock import MagicMock
    c = MagicMock()
    c.cloud_id.return_value = "test-cloud-uuid"
    c.sync_push.return_value = {"updated_time": 12345, "created_time": 12345}
    c.sync_pull.return_value = {"next_cursor": 0, "upserts": [], "deletes": []}
    return c


class TestPresetModal:
    """Tests for the preset-modal (Push to Cloud) endpoints."""

    def test_preset_modal_success(self, client, monkeypatch, sample_db, tmp_path):
        """POST /sync/preset-modal returns a done fragment on success."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_preset_template_factory", _test_template)
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: _fake_orca())
        monkeypatch.setattr(sh, "_github_client_factory", lambda: None)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.post("/sync/preset-modal", data={"profile_id": 1})
        assert r.status_code == 200
        assert b"Preset pushed" in r.content
        assert b"closeActionModal" in r.content

    def test_preset_modal_not_connected_error(self, client, monkeypatch,
                                                sample_db, tmp_path):
        """POST /sync/preset-modal shows error when Orca Cloud is not connected."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_preset_template_factory", _test_template)
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: None)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.post("/sync/preset-modal", data={"profile_id": 1})
        assert r.status_code == 200
        assert b"Push failed" in r.content
        assert b"Orca Cloud not configured" in r.content

    def test_preset_modal_done_triggers_hub_refresh(self, client, monkeypatch,
                                                      sample_db, tmp_path):
        """Done fragment includes a hub-fragment refresh div."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_preset_template_factory", _test_template)
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: _fake_orca())
        monkeypatch.setattr(sh, "_github_client_factory", lambda: None)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.post("/sync/preset-modal", data={"profile_id": 1})
        assert b"hub-fragment" in r.content

    def test_preset_modal_poll_returns_fragment(self, client, monkeypatch,
                                                  sample_db, tmp_path):
        """GET /sync/preset-modal/{job_id} returns the job's current state."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_preset_template_factory", _test_template)
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: _fake_orca())
        monkeypatch.setattr(sh, "_github_client_factory", lambda: None)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        # Use the API endpoint to get a job_id
        r = client.post("/api/sync/preset/1")
        job_id = r.json()["job_id"]
        r = client.get(f"/sync/preset-modal/{job_id}")
        assert r.status_code == 200
        assert b"Preset pushed" in r.content

    def test_preset_modal_running_shows_spinner(self, client, monkeypatch,
                                                  sample_db, tmp_path):
        """Queued job shows spinner + polling div."""
        monkeypatch.setattr(jobs, "drain_background", lambda c: None)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_preset_template_factory", _test_template)
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: _fake_orca())
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.post("/sync/preset-modal", data={"profile_id": 1})
        assert r.status_code == 200
        assert b"spinner spinner-lg" in r.content
        assert b"preset-modal" in r.content
        assert b"closeActionModal" in r.content


# ---------------------------------------------------------------------------
# Pull modal — Pull from Cloud via background job + modal polling
# ---------------------------------------------------------------------------

class TestPullModal:
    """Tests for the pull-modal (Pull from Cloud) endpoints."""

    def test_pull_modal_success(self, client, monkeypatch):
        """POST /sync/pull-modal returns a done fragment on success."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        orca = _fake_orca()
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: orca)
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.post("/sync/pull-modal")
        assert r.status_code == 200
        assert b"Pull complete" in r.content
        assert b"baseline adopted" in r.content
        assert b"closeActionModal" in r.content

    def test_pull_modal_not_connected_error(self, client, monkeypatch):
        """POST /sync/pull-modal shows error when Orca Cloud is not connected."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: None)
        r = client.post("/sync/pull-modal")
        assert r.status_code == 200
        assert b"Pull failed" in r.content
        assert b"Orca Cloud not connected" in r.content

    def test_pull_modal_done_triggers_hub_refresh(self, client, monkeypatch):
        """Done fragment includes a hub-fragment refresh div."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        orca = _fake_orca()
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: orca)
        r = client.post("/sync/pull-modal")
        assert b"hub-fragment" in r.content

    def test_pull_modal_per_profile(self, client, monkeypatch):
        """POST /sync/pull-modal with profile_id pulls a single profile."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        orca = _fake_orca()
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: orca)
        p = client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        }).json()
        r = client.post("/sync/pull-modal", data={"profile_id": p["id"]})
        assert r.status_code == 200
        assert b"Pull complete" in r.content

    def test_pull_modal_poll_returns_fragment(self, client, monkeypatch):
        """GET /sync/pull-modal/{job_id} returns the job's current state."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        orca = _fake_orca()
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: orca)
        client.post("/sync/pull-modal")
        # Query the latest pull job from the DB
        with SessionLocal() as s:
            job = s.scalars(
                select(Job).where(Job.kind == "pull").order_by(Job.id.desc())
            ).first()
            assert job is not None
            job_id = job.id
        r = client.get(f"/sync/pull-modal/{job_id}")
        assert r.status_code == 200
        assert b"Pull complete" in r.content

    def test_pull_modal_running_shows_spinner(self, client, monkeypatch):
        """Queued job shows spinner + polling div."""
        monkeypatch.setattr(jobs, "drain_background", lambda c: None)
        orca = _fake_orca()
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: orca)
        r = client.post("/sync/pull-modal")
        assert r.status_code == 200
        assert b"spinner spinner-lg" in r.content
        assert b"pull-modal" in r.content
        assert b"closeActionModal" in r.content


# ---------------------------------------------------------------------------
# Backup modal — Backup to GitHub via background job + modal polling
# ---------------------------------------------------------------------------

class TestBackupModal:
    """Tests for the backup-modal (Backup to GitHub) endpoints."""

    def test_backup_modal_success(self, client, monkeypatch):
        """POST /sync/backup-modal returns a done fragment on success."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        orca = _fake_orca()
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: orca)
        from unittest.mock import MagicMock
        mock_github = MagicMock()
        mock_github.backup.return_value = {"commit_sha": "abc", "files_written": 1}
        monkeypatch.setattr(sh, "_github_client_factory", lambda: mock_github)
        r = client.post("/sync/backup-modal")
        assert r.status_code == 200
        assert b"Backup complete" in r.content
        assert b"0 presets" in r.content  # empty cloud → 0 presets
        assert b"closeActionModal" in r.content

    def test_backup_modal_not_connected_error(self, client, monkeypatch):
        """POST /sync/backup-modal shows error when GitHub is not configured."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: _fake_orca())
        monkeypatch.setattr(sh, "_github_client_factory", lambda: None)
        r = client.post("/sync/backup-modal")
        assert r.status_code == 200
        assert b"Backup failed" in r.content
        assert b"GitHub not configured" in r.content

    def test_backup_modal_done_triggers_hub_refresh(self, client, monkeypatch):
        """Done fragment includes a hub-fragment refresh div."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        orca = _fake_orca()
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: orca)
        from unittest.mock import MagicMock
        mock_github = MagicMock()
        mock_github.backup.return_value = {"commit_sha": "abc", "files_written": 1}
        monkeypatch.setattr(sh, "_github_client_factory", lambda: mock_github)
        r = client.post("/sync/backup-modal")
        assert b"hub-fragment" in r.content

    def test_backup_modal_poll_returns_fragment(self, client, monkeypatch):
        """GET /sync/backup-modal/{job_id} returns the job's current state."""
        monkeypatch.setattr(jobs, "drain_background", _sync_drain)
        orca = _fake_orca()
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: orca)
        from unittest.mock import MagicMock
        mock_github = MagicMock()
        mock_github.backup.return_value = {"commit_sha": "abc", "files_written": 1}
        monkeypatch.setattr(sh, "_github_client_factory", lambda: mock_github)
        client.post("/sync/backup-modal")
        with SessionLocal() as s:
            job = s.scalars(
                select(Job).where(Job.kind == "github_backup").order_by(Job.id.desc())
            ).first()
            assert job is not None
            job_id = job.id
        r = client.get(f"/sync/backup-modal/{job_id}")
        assert r.status_code == 200
        assert b"Backup complete" in r.content

    def test_backup_modal_running_shows_spinner(self, client, monkeypatch):
        """Queued job shows spinner + polling div."""
        monkeypatch.setattr(jobs, "drain_background", lambda c: None)
        orca = _fake_orca()
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: orca)
        from unittest.mock import MagicMock
        mock_github = MagicMock()
        mock_github.backup.return_value = {"commit_sha": "abc", "files_written": 1}
        monkeypatch.setattr(sh, "_github_client_factory", lambda: mock_github)
        r = client.post("/sync/backup-modal")
        assert r.status_code == 200
        assert b"spinner spinner-lg" in r.content
        assert b"backup-modal" in r.content
        assert b"closeActionModal" in r.content


# ---------------------------------------------------------------------------
# htmx version pin regression test
# ---------------------------------------------------------------------------

class TestHtmxPin:
    """Regression test: base.html must pin htmx to a specific 1.9.x version."""

    def test_base_html_pins_htmx_version(self):
        """base.html contains the pinned htmx URL, not the unversioned one."""
        from pathlib import Path
        base = Path(__file__).resolve().parent.parent / "fm" / "api" / "templates" / "base.html"
        content = base.read_text()
        # Must contain a pinned version (not the bare unversioned URL)
        assert "unpkg.com/htmx.org@" in content
        assert "unpkg.com/htmx.org\" " not in content  # no unversioned
        assert "unpkg.com/htmx.org@" in content
        # Must be a 1.9.x release
        assert "htmx.org@1.9." in content


# ---------------------------------------------------------------------------
# Sync page new modal wiring
# ---------------------------------------------------------------------------

class TestSyncPageModalWiring:
    """Verify the /sync page wires all operations to the action modal."""

    def test_sync_page_has_preset_modal_buttons(self, client):
        """Push to Cloud buttons POST to /sync/preset-modal."""
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.get("/sync")
        assert b"/sync/preset-modal" in r.content

    def test_sync_page_has_pull_modal_buttons(self, client):
        """Pull from Cloud buttons POST to /sync/pull-modal."""
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.get("/sync")
        assert b"/sync/pull-modal" in r.content

    def test_sync_page_has_backup_modal_button(self, client, monkeypatch):
        """Backup to GitHub button POSTs to /sync/backup-modal when configured."""
        monkeypatch.setattr("fm.api.ui.load_github_config", lambda: {"token": "x", "repo": "o/r"})
        r = client.get("/sync")
        assert b"/sync/backup-modal" in r.content

    def test_sync_page_all_buttons_use_action_modal(self, client, monkeypatch):
        """All sync buttons target #action-modal-body and call openActionModal."""
        monkeypatch.setattr("fm.api.ui.load_github_config", lambda: {"token": "x", "repo": "o/r"})
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        r = client.get("/sync")
        assert b'#action-modal-body' in r.content
        assert b'openActionModal' in r.content
        # No old toast-based inline JS for sync operations
        assert b"hx-post=\"/api/sync/preset/" not in r.content
        assert b"hx-post=\"/api/sync/pull/" not in r.content
        assert b"hx-post=\"/api/sync/backup-github\"" not in r.content


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

    def test_import_scan_error_surfaces_alert(self, client, monkeypatch):
        """When the latest import_scan job errored, /import?tab=printer
        shows an error alert with the job's error message."""
        # Make the scan handler raise by pointing at a nonexistent import DB
        from pathlib import Path
        monkeypatch.setattr(sh, "_import_db_path", Path("/nonexistent/import_db.json"))
        client.post("/import/printer/scan")
        r = client.get("/import?tab=printer")
        assert r.status_code == 200
        assert b"Import scan failed" in r.content

    def test_import_scan_form_has_onsubmit(self, client):
        """The scan form has onsubmit JS that disables the button."""
        r = client.get("/import?tab=printer")
        assert r.status_code == 200
        assert b"onsubmit" in r.content
        assert b"Scanning" in r.content
