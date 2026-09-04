# tests/test_ui.py
"""UI page smoke tests — content-marker level only.

Visual/layout refinement is a separate designer pass; these tests assert that
the functional pages render the expected structure and that form submissions
round-trip through the page routes (not the JSON API).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from fm.db import SessionLocal
from fm.models import ProfileCache


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/ui.db")
    monkeypatch.setenv("FM_SETTINGS_PATH", str(tmp_path / "settings.json"))
    from fm.api.app import create_app
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def seeded_cache_profile(client) -> str:
    """Insert a ProfileCache row and return its cloud_resource_id."""
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        pc = ProfileCache(
            cloud_resource_id="uuid-test-1",
            name="Sunlu PLA+ Optimized",
            vendor="Sunlu",
            type="PLA",
            is_inherited=False,
            deleted_from_cloud=False,
            last_seen_at=now,
        )
        s.add(pc)
        s.commit()
        return pc.cloud_resource_id


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Dashboard" in r.content


def test_dashboard_sections_present(client):
    r = client.get("/")
    assert b"preset-sync-status" in r.content
    assert b"recent-activity" not in r.content


def test_sync_page_no_job_progress(client):
    r = client.get("/sync")
    assert b"job-progress" not in r.content


# ---------------------------------------------------------------------------
# Spools list + add form + HTMX remaining patch
# ---------------------------------------------------------------------------

def test_spools_list_renders(client):
    r = client.get("/spools")
    assert r.status_code == 200
    assert b"Add spool" in r.content


def test_spool_create_via_form(client, seeded_cache_profile):
    """POST /spools/form → redirect + row visible on the spools page."""
    r = client.post("/spools/form", data={
        "cloud_resource_id": seeded_cache_profile,
        "remaining_pct": "100",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b"Sunlu PLA+ Optimized" in r.content


def test_spool_remaining_patch_404(client):
    r = client.post("/spools/9999/remaining", data={"remaining_pct": "10"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Fix-round-1 regressions
# ---------------------------------------------------------------------------

def test_spools_filter_empty_cloud_resource_id(client):
    """?cloud_resource_id= (the '(all)' default) must return 200, not 422."""
    r = client.get("/spools?cloud_resource_id=")
    assert r.status_code == 200
    assert b"Add spool" in r.content


# ---------------------------------------------------------------------------
# Fix-round-2 regressions
# ---------------------------------------------------------------------------

def test_spools_filter_nonexistent_cloud_resource_id(client):
    """?cloud_resource_id=nonexistent must not 500 — treat as no matches (200)."""
    r = client.get("/spools?cloud_resource_id=nonexistent")
    assert r.status_code == 200
    assert b"Add spool" in r.content


# ---------------------------------------------------------------------------
# Sync Hub redesign — label cleanup
# ---------------------------------------------------------------------------

def test_no_old_labels_in_sync_page(client):
    """Sync Hub page must not contain any old labels."""
    r = client.get("/sync")
    assert r.status_code == 200
    assert b"Sync from Orca" not in r.content
    assert b"Generate preset" not in r.content


# ---------------------------------------------------------------------------
# Navigation structure
# ---------------------------------------------------------------------------

def test_nav_has_five_items(client):
    """base.html nav has exactly 5 items (Dashboard, Spools, Sync, Logs, Settings)."""
    r = client.get("/")
    content = r.text
    assert content.count('class="nav-item') == 5 or content.count('class="nav-item active') + content.count('class="nav-item"') == 5


def test_nav_no_profiles(client):
    """Nav does not contain 'Profiles' link."""
    r = client.get("/")
    assert b'href="/profiles"' not in r.content


def test_nav_no_import(client):
    """Nav does not contain 'Import' link."""
    r = client.get("/")
    assert b'href="/import"' not in r.content


def test_nav_no_migration(client):
    """Nav does not contain 'Migration'."""
    r = client.get("/")
    assert b"Migration" not in r.content


def test_nav_no_orca_cloud_import(client):
    """Nav does not contain 'Orca Cloud Import'."""
    r = client.get("/")
    assert b"Orca Cloud Import" not in r.content


# ---------------------------------------------------------------------------
# Fix-wave: dashboard queries ProfileCache, total spools, printer DB targets
# ---------------------------------------------------------------------------

def test_dashboard_shows_cached_presets_count(client, seeded_cache_profile):
    """Dashboard displays 'Cached presets' label and count from ProfileCache."""
    r = client.get("/")
    assert r.status_code == 200
    assert b"Cached presets" in r.content
    assert b"Spools in stock" in r.content
    assert b"Printer DB synced" in r.content


def test_dashboard_cached_presets_excludes_deleted(client):
    """Deleted-from-cloud presets are not counted in the cached presets stat."""
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        s.add(ProfileCache(
            cloud_resource_id="uuid-active",
            name="Active Preset",
            vendor="Sunlu",
            type="PLA",
            deleted_from_cloud=False,
            last_seen_at=now,
        ))
        s.add(ProfileCache(
            cloud_resource_id="uuid-deleted",
            name="Deleted Preset",
            vendor="Sunlu",
            type="PLA",
            deleted_from_cloud=True,
            deleted_at=now,
            last_seen_at=now,
        ))
        s.commit()
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Cached presets" in html
    # Only 1 active preset should be counted
    assert ">1<" in html
    assert ">2<" not in html


# ---------------------------------------------------------------------------
# v2.0.0 dashboard modernization — ProfileCache spools, preset sync, no doubles
# ---------------------------------------------------------------------------

def test_dashboard_no_double_names(client, seeded_cache_profile):
    """Dashboard must not print 'Sunlu Sunlu PLA+ Optimized' — the name already
    starts with the vendor, so the brand prefix is deduplicated."""
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Sunlu PLA+ Optimized" in html
    assert "Sunlu Sunlu" not in html


def test_dashboard_spools_by_preset_header(client):
    """The spool summary panel header reads 'Spools by preset' (not 'profile')."""
    r = client.get("/")
    assert b"Spools by preset" in r.content


def test_dashboard_no_drift_column(client):
    """The obsolete 'Sync drift' / 'Printer | Orca' matrix must be gone."""
    r = client.get("/")
    assert b"Sync drift" not in r.content
    assert b"drift-matrix" not in r.content


def test_dashboard_preset_sync_status_table(client, seeded_cache_profile):
    """The right panel shows 'Preset sync status' with Preset/Printer DB/Status
    columns and the cached preset row."""
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Preset sync status" in html
    assert "Printer DB" in html
    # The seeded preset appears with deduplicated name
    assert "Sunlu PLA+ Optimized" in html


def test_dashboard_spool_counts_via_cloud_resource_id(client, seeded_cache_profile):
    """Spools linked via cloud_resource_id are counted in the spool summary."""
    from fm.models import Spool
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        s.add(Spool(
            cloud_resource_id=seeded_cache_profile,
            remaining_pct=100,
            created_at=now,
            updated_at=now,
        ))
        s.commit()
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Sunlu PLA+ Optimized" in html
    assert "1 spool" in html


def test_dashboard_empty_presets_state(client):
    """With no cached presets, the preset sync panel shows the empty state."""
    r = client.get("/")
    assert r.status_code == 200
    assert b"No cached presets." in r.content


# ---------------------------------------------------------------------------
# Logs page — unified timeline, filtering, search, sort, dedup
# ---------------------------------------------------------------------------

from fm.models import ActivityLog, Job


def _seed_job(db, kind="push", status="done", result=None, payload=None):
    """Insert a Job row and return it."""
    now = datetime.now(timezone.utc)
    job = Job(
        kind=kind,
        payload=json.dumps(payload or {}),
        status=status,
        result=json.dumps(result) if result else None,
        created_at=now,
        finished_at=now if status in ("done", "error") else None,
    )
    db.add(job)
    db.flush()
    return job


def _seed_activity(db, action="cache_sync", outcome="ok", detail=None):
    """Insert an ActivityLog row and return it."""
    act = ActivityLog(
        ts=datetime.now(timezone.utc),
        action=action,
        outcome=outcome,
        detail=json.dumps(detail or {}),
    )
    db.add(act)
    db.flush()
    return act


def test_logs_page_loads(client):
    """GET /logs returns 200, has page structure, nav item active."""
    r = client.get("/logs")
    assert r.status_code == 200
    assert b"Logs" in r.content
    assert b"logs-page" in r.content
    assert b"logs-table-container" in r.content
    # Nav item is active on /logs
    assert b'href="/logs"' in r.content
    assert b'nav-item active' in r.content


def test_logs_table_partial(client):
    """GET /logs/table returns 200 with table or empty-state."""
    r = client.get("/logs/table")
    assert r.status_code == 200
    # With no data, should show empty state
    assert b"No log entries yet" in r.content


def test_logs_filter_status(client):
    """Filter by status works — only matching entries returned."""
    with SessionLocal() as s:
        _seed_job(s, kind="push", status="running")
        _seed_job(s, kind="verify", status="done")
        s.commit()

    r = client.get("/logs/table?status=running")
    assert r.status_code == 200
    assert b"running" in r.content
    # The done job should not appear when filtering for running
    html = r.text
    # Count log rows — should be 1 (only the running job)
    assert html.count('class="log-row ') == 1


def test_logs_filter_source(client):
    """Filter by source=job vs source=action works."""
    with SessionLocal() as s:
        _seed_job(s, kind="push", status="queued")
        _seed_activity(s, action="cache_sync", outcome="ok")
        s.commit()

    r_job = client.get("/logs/table?source=job")
    assert r_job.status_code == 200
    assert b"badge-purple" in r_job.content  # Job badge
    assert b"badge-muted" not in r_job.content  # No Action badge

    r_act = client.get("/logs/table?source=action")
    assert r_act.status_code == 200
    assert b"badge-muted" in r_act.content  # Action badge
    assert b"badge-purple" not in r_act.content  # No Job badge


def test_logs_search(client):
    """Search q filters by action/payload/error."""
    with SessionLocal() as s:
        _seed_job(s, kind="push", status="done", payload={"target": "192.168.1.50"})
        _seed_job(s, kind="verify", status="done")
        s.commit()

    # Search for "push" — should match the push job
    r = client.get("/logs/table?q=push")
    assert r.status_code == 200
    assert b"Push to printer" in r.content
    # Verify job should not appear
    assert b"Verify printer DB" not in r.content


def test_logs_sort(client):
    """sort=asc vs desc orders by timestamp."""
    from datetime import timedelta
    base = datetime.now(timezone.utc)
    with SessionLocal() as s:
        old = Job(kind="push", payload="{}", status="done",
                  created_at=base - timedelta(hours=2),
                  finished_at=base - timedelta(hours=2))
        new = Job(kind="verify", payload="{}", status="done",
                  created_at=base,
                  finished_at=base)
        s.add_all([old, new])
        s.commit()

    r_desc = client.get("/logs/table?sort=desc")
    html_desc = r_desc.text
    pos_verify = html_desc.find("Verify printer DB")
    pos_push = html_desc.find("Push to printer")
    assert pos_verify < pos_push  # newer first

    r_asc = client.get("/logs/table?sort=asc")
    html_asc = r_asc.text
    pos_verify = html_asc.find("Verify printer DB")
    pos_push = html_asc.find("Push to printer")
    assert pos_push < pos_verify  # older first


def test_logs_deduplication(client):
    """A completed Job with a matching ActivityLog entry produces only ONE
    unified entry, not two."""
    with SessionLocal() as s:
        job = _seed_job(s, kind="push", status="done",
                        result={"pushed": 3})
        _seed_activity(s, action="push", outcome="ok",
                       detail={"job_id": job.id, "result": {"pushed": 3}})
        s.commit()

    r = client.get("/logs/table")
    assert r.status_code == 200
    html = r.text
    # Should have exactly 1 log row (the activity_log entry, not the job)
    assert html.count('class="log-row ') == 1
    # The entry should be the action (ActivityLog), not the job
    assert b"badge-muted" in r.content
    assert b"badge-purple" not in r.content
