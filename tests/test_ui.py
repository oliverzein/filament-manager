# tests/test_ui.py
"""UI page smoke tests — content-marker level only.

Visual/layout refinement is a separate designer pass; these tests assert that
the functional pages render the expected structure and that form submissions
round-trip through the page routes (not the JSON API).
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from fm.db import SessionLocal
from fm.models import Profile, SyncTarget
from fm.service import sync_handlers as sh


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/ui.db")
    from fm.api.app import create_app
    with TestClient(create_app()) as c:
        yield c


def _make_profile(client, **overrides) -> dict:
    payload = {
        "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
        "min_temp": 200, "max_temp": 240, "density": 1.24,
        "drying_temp": 60, "drying_time": 8,
    }
    payload.update(overrides)
    r = client.post("/api/profiles", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def seeded_profile(client) -> dict:
    return _make_profile(client)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Spools" in r.content
    assert b"Dashboard" in r.content


def test_dashboard_drift_and_activity_sections_present(client):
    r = client.get("/")
    assert b"drift-matrix" in r.content
    assert b"recent-activity" in r.content


# ---------------------------------------------------------------------------
# Profiles list + form
# ---------------------------------------------------------------------------

def test_profiles_list_renders(client, seeded_profile):
    r = client.get("/profiles")
    assert r.status_code == 200
    assert b"Sunlu PLA+" in r.content


def test_profiles_filter_by_type(client):
    _make_profile(client, brand="eSUN", name="eSUN PETG Basic", type="PETG")
    r = client.get("/profiles?type=PETG")
    assert r.status_code == 200
    assert b"eSUN PETG Basic" in r.content
    assert b"Sunlu PLA+" not in r.content


def test_profile_form_groups(client):
    """The new-profile form must render identity/temps/drying groups."""
    r = client.get("/profiles/new")
    assert r.status_code == 200
    assert b"Identity" in r.content
    assert b"Temperature" in r.content
    assert b"Drying" in r.content


def test_profile_edit_form_preloads(client, seeded_profile):
    r = client.get(f"/profiles/{seeded_profile['id']}/edit")
    assert r.status_code == 200
    assert b"Sunlu PLA+" in r.content
    # validation-warnings hook present on edit
    assert b"validation-warnings" in r.content


def test_profile_create_via_form(client):
    r = client.post("/profiles/form", data={
        "brand": "Polymaker", "name": "Polymaker PETG", "type": "PETG",
        "colors": "#ff8800", "min_temp": "220", "max_temp": "260",
        "density": "1.27", "drying_temp": "70", "drying_time": "8",
        "notes": "via form",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b"Polymaker PETG" in r.content


def test_profile_create_via_form_validation_error(client):
    """Bad temp range re-renders the form with errors (no redirect)."""
    r = client.post("/profiles/form", data={
        "brand": "Bad", "name": "Bad Bad", "type": "PLA",
        "min_temp": "300", "max_temp": "200",
    })
    assert r.status_code == 422
    assert b"Identity" in r.content  # form re-rendered


# ---------------------------------------------------------------------------
# Spools list + add form + HTMX remaining patch
# ---------------------------------------------------------------------------

def test_spools_list_renders(client, seeded_profile):
    r = client.get("/spools")
    assert r.status_code == 200
    assert b"Add spool" in r.content


def test_spool_create_via_form(client, seeded_profile):
    """POST /spools/form → redirect + row visible on the spools page."""
    r = client.post("/spools/form", data={
        "profile_id": str(seeded_profile["id"]),
        "color_override": "#112233",
        "remaining_pct": "80",
        "location": "shelf A",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b"shelf A" in r.content
    assert b"80%" in r.content


    r = client.post("/spools/form", data={
        "profile_id": str(seeded_profile["id"]),
        "remaining_pct": "50",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/spools"


def test_spool_remaining_htmx_patch(client, seeded_profile):
    s = client.post("/api/spools", json={"profile_id": seeded_profile["id"]}).json()
    r = client.post(f"/spools/{s['id']}/remaining", data={"remaining_pct": "42"})
    assert r.status_code == 200
    assert b"42%" in r.content
    # persisted — read back via the list endpoint (no single-spool GET exists)
    spool = next(x for x in client.get("/api/spools").json() if x["id"] == s["id"])
    assert spool["remaining_pct"] == 42


def test_spool_remaining_patch_404(client):
    r = client.post("/spools/9999/remaining", data={"remaining_pct": "10"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Fix-round-1 regressions
# ---------------------------------------------------------------------------

def test_spools_filter_empty_profile_id(client, seeded_profile):
    """?profile_id= (the '(all)' default) must return 200, not 422."""
    r = client.get("/spools?profile_id=&location=")
    assert r.status_code == 200
    assert b"Add spool" in r.content


def test_profile_edit_colors_rendered_from_json(client):
    """Profile.colors is a JSON string; the edit form must render the parsed
    list joined as comma-separated hex, not the raw JSON characters."""
    p = _make_profile(client, colors=["#ff0000", "#00ff00"])
    r = client.get(f"/profiles/{p['id']}/edit")
    assert r.status_code == 200
    assert b'value="#ff0000, #00ff00"' in r.content


def test_profile_edit_colors_empty_array_blank(client):
    """A profile with no colors renders an empty colors input value."""
    p = _make_profile(client)  # default colors=[]
    r = client.get(f"/profiles/{p['id']}/edit")
    assert r.status_code == 200
    assert b'value=""' in r.content


# ---------------------------------------------------------------------------
# Fix-round-2 regressions
# ---------------------------------------------------------------------------

def test_spools_filter_non_numeric_profile_id(client, seeded_profile):
    """?profile_id=abc must not 500 — treat as no filter (200)."""
    r = client.get("/spools?profile_id=abc")
    assert r.status_code == 200
    assert b"Add spool" in r.content


# ---------------------------------------------------------------------------
# Sync Hub redesign — label cleanup
# ---------------------------------------------------------------------------

def test_profiles_page_no_sync_from_orca(client, seeded_profile):
    """Profiles page must not contain 'Sync from Orca' button."""
    r = client.get("/profiles")
    assert r.status_code == 200
    assert b"Sync from Orca" not in r.content
    # Should have a link to sync hub instead
    assert b"Sync status" in r.content or b"/sync" in r.content


def test_profiles_page_no_orca_toast(client, seeded_profile):
    """Profiles page must not contain the Orca toast script."""
    r = client.get("/profiles")
    assert b"Synced from Orca Cloud" not in r.content


def test_orca_fragment_has_pull_from_cloud_label(client, seeded_profile):
    """Orca fragment button says 'Pull from Cloud', not 'Sync from Orca'."""
    r = client.get(f"/profiles/{seeded_profile['id']}/orca-fragment")
    assert r.status_code == 200
    assert b"Pull from Cloud" in r.content
    assert b"Sync from Orca" not in r.content



# ---------------------------------------------------------------------------
# V10 — Orca fragment status-dependent source line
# ---------------------------------------------------------------------------

_PRESET_JSON = '{"nozzle_temperature": ["220"], "filament_flow_ratio": ["0.98"]}'


def _add_orca_target(profile_id: int, status: str, actual_state: str | None = None):
    """Insert an orca_preset SyncTarget for the given profile."""
    with SessionLocal() as s:
        s.add(SyncTarget(
            profile_id=profile_id,
            kind="orca_preset",
            cloud_id="test-cloud-uuid",
            expected_hash="some_hash",
            actual_state=actual_state,
            status=status,
        ))
        s.commit()


def test_orca_fragment_in_sync_status_line(client, seeded_profile):
    """Fragment shows the in_sync source line when target.status is in_sync."""
    _add_orca_target(seeded_profile["id"], "in_sync", _PRESET_JSON)
    r = client.get(f"/profiles/{seeded_profile['id']}/orca-fragment")
    assert r.status_code == 200
    assert "Matches the catalog — as pushed or adopted." in r.text


def test_orca_fragment_drift_status_line(client, seeded_profile):
    """Fragment shows the drift source line when target.status is drift."""
    _add_orca_target(seeded_profile["id"], "drift", _PRESET_JSON)
    r = client.get(f"/profiles/{seeded_profile['id']}/orca-fragment")
    assert r.status_code == 200
    assert "Cloud values differ from the catalog — tuned in OrcaSlicer? Pull from Cloud adopts them." in r.text


def test_orca_fragment_pending_status_line(client, seeded_profile):
    """Fragment shows the pending source line when target.status is pending."""
    _add_orca_target(seeded_profile["id"], "pending", _PRESET_JSON)
    r = client.get(f"/profiles/{seeded_profile['id']}/orca-fragment")
    assert r.status_code == 200
    assert "No baseline yet — push to Cloud or pull once." in r.text


def test_orca_fragment_no_status_line_without_target(client, seeded_profile):
    """Fragment shows none of the status source lines when no SyncTarget exists."""
    r = client.get(f"/profiles/{seeded_profile['id']}/orca-fragment")
    assert r.status_code == 200
    assert "Matches the catalog" not in r.text
    assert "Cloud values differ" not in r.text


# ---------------------------------------------------------------------------
# V12 — Fragment error note when pulling without Orca Cloud connection
# ---------------------------------------------------------------------------

def test_sync_from_orca_error_note_without_connection(client, seeded_profile, monkeypatch):
    """POST /profiles/{pid}/sync-from-orca renders error note (not success toast)
    when Orca Cloud is not connected."""
    _add_orca_target(seeded_profile["id"], "in_sync", _PRESET_JSON)
    monkeypatch.setattr(sh, "_orca_client_factory", lambda: None)
    r = client.post(f"/profiles/{seeded_profile['id']}/sync-from-orca")
    assert r.status_code == 200
    assert "Orca Cloud not connected — set up the connection in Settings" in r.text
    assert "Synced from Orca Cloud" not in r.text  # no success toast


def test_sync_from_orca_error_note_without_connection_no_target(client, seeded_profile, monkeypatch):
    """POST /profiles/{pid}/sync-from-orca renders error note in empty-state branch
    when no SyncTarget exists and Orca Cloud is not connected."""
    monkeypatch.setattr(sh, "_orca_client_factory", lambda: None)
    r = client.post(f"/profiles/{seeded_profile['id']}/sync-from-orca")
    assert r.status_code == 200
    assert "Orca Cloud not connected — set up the connection in Settings" in r.text
    assert "Synced from Orca Cloud" not in r.text
    assert "No baseline yet" not in r.text

def test_profiles_sync_from_orca_route_removed(client):
    """POST /profiles/sync-from-orca returns 404 (route removed)."""
    r = client.post("/profiles/sync-from-orca")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Navigation structure
# ---------------------------------------------------------------------------

def test_nav_has_six_items(client):
    """base.html nav has exactly 6 items (not 7)."""
    r = client.get("/")
    # Count nav-item elements
    content = r.text
    assert content.count('class="nav-item') == 6 or content.count('class="nav-item active') + content.count('class="nav-item"') == 6


def test_nav_has_import(client):
    """Nav contains 'Import' linking to /import."""
    r = client.get("/")
    assert b"Import" in r.content
    assert b'href="/import"' in r.content


def test_nav_no_migration(client):
    """Nav does not contain 'Migration'."""
    r = client.get("/")
    assert b"Migration" not in r.content


def test_nav_no_orca_cloud_import(client):
    """Nav does not contain 'Orca Cloud Import'."""
    r = client.get("/")
    assert b"Orca Cloud Import" not in r.content


def test_no_old_labels_in_sync_page(client):
    """Sync Hub page must not contain any old labels."""
    r = client.get("/sync")
    assert b"Sync from Orca" not in r.content
    assert b"Push all profiles" not in r.content
    assert b"Sync now" not in r.content
    assert b"Generate preset" not in r.content


def test_no_old_labels_in_profiles_page(client, seeded_profile):
    """Profiles page must not contain old labels."""
    r = client.get("/profiles")
    assert b"Sync from Orca" not in r.content
