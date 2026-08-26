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
from fm.models import Profile


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
    """The new-profile form must render identity/temps/extrusion/drying groups."""
    r = client.get("/profiles/new")
    assert r.status_code == 200
    assert b"Identity" in r.content
    assert b"Temperature" in r.content
    assert b"Extrusion" in r.content
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
