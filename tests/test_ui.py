# tests/test_ui.py
"""UI page smoke tests — content-marker level only.

Visual/layout refinement is a separate designer pass; these tests assert that
the functional pages render the expected structure and that form submissions
round-trip through the page routes (not the JSON API).
"""
from __future__ import annotations

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


def test_dashboard_preset_sync_and_activity_sections_present(client):
    r = client.get("/")
    assert b"preset-sync-status" in r.content
    assert b"recent-activity" in r.content


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

def test_nav_has_four_items(client):
    """base.html nav has exactly 4 items (Dashboard, Spools, Sync, Settings)."""
    r = client.get("/")
    content = r.text
    assert content.count('class="nav-item') == 4 or content.count('class="nav-item active') + content.count('class="nav-item"') == 4


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
