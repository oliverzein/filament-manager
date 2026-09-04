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


def test_dashboard_drift_and_activity_sections_present(client):
    r = client.get("/")
    assert b"drift-matrix" in r.content
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
