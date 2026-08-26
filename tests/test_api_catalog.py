# tests/test_api_catalog.py
"""Catalog CRUD API tests — httpx/TestClient against create_app() with a
per-test tmp SQLite via FM_DATABASE_URL."""
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
    monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/api.db")
    from fm.api.app import create_app
    with TestClient(create_app()) as c:
        yield c


def _make_profile(client, **overrides) -> dict:
    payload = {
        "brand": "Sunlu",
        "name": "Sunlu PLA+",
        "type": "PLA",
        "min_temp": 200,
        "max_temp": 230,
        "density": 1.24,
    }
    payload.update(overrides)
    r = client.post("/api/profiles", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def seeded_profile(client) -> dict:
    return _make_profile(client)


@pytest.fixture
def profile_with_printer(client) -> dict:
    """A profile that has been pushed to the printer DB (printer_db_id set)."""
    p = _make_profile(client, name="Sunlu PLA+")
    with SessionLocal() as s:
        row = s.get(Profile, p["id"])
        row.printer_db_id = "99001"
    return p


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

def test_api_version(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    assert "version" in r.json()


# ---------------------------------------------------------------------------
# Profile CRUD
# ---------------------------------------------------------------------------

def test_create_profile(client):
    p = _make_profile(client, colors=["#ff0000", "#00ff00"])
    assert p["id"] >= 1
    assert p["brand"] == "Sunlu"
    assert p["colors"] == ["#ff0000", "#00ff00"]
    assert p["created_at"] == p["updated_at"]


def test_create_profile_rejects_bad_temps(client):
    r = client.post("/api/profiles", json={
        "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
        "min_temp": 400, "max_temp": 200,
    })
    assert r.status_code == 422


def test_create_profile_rejects_temp_out_of_range(client):
    r = client.post("/api/profiles", json={
        "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
        "min_temp": 50, "max_temp": 200,
    })
    assert r.status_code == 422


def test_create_profile_rejects_missing_required(client):
    r = client.post("/api/profiles", json={"brand": "Sunlu", "name": "Sunlu PLA+"})
    assert r.status_code == 422


def test_create_profile_rejects_bad_density(client):
    r = client.post("/api/profiles", json={
        "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
        "min_temp": 200, "max_temp": 230, "density": 5.0,
    })
    assert r.status_code == 422


def test_list_profiles(client, seeded_profile):
    r = client.get("/api/profiles")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["id"] == seeded_profile["id"]


def test_get_profile(client, seeded_profile):
    r = client.get(f"/api/profiles/{seeded_profile['id']}")
    assert r.status_code == 200
    assert r.json()["name"] == "Sunlu PLA+"


def test_get_profile_404(client):
    assert client.get("/api/profiles/9999").status_code == 404


def test_patch_profile_partial(client, seeded_profile):
    r = client.patch(f"/api/profiles/{seeded_profile['id']}", json={"notes": "updated"})
    assert r.status_code == 200
    assert r.json()["notes"] == "updated"
    assert r.json()["brand"] == "Sunlu"  # unchanged
    assert r.json()["updated_at"] >= seeded_profile["updated_at"]


def test_patch_profile_validates_merged_temps(client, seeded_profile):
    """Raising min_temp above existing max_temp must fail."""
    r = client.patch(f"/api/profiles/{seeded_profile['id']}", json={"min_temp": 250})
    # existing max_temp is 230 → 250 > 230 → 422
    assert r.status_code == 422


def test_patch_profile_404(client):
    assert client.patch("/api/profiles/9999", json={"notes": "x"}).status_code == 404


def test_patch_profile_colors(client, seeded_profile):
    r = client.patch(f"/api/profiles/{seeded_profile['id']}", json={"colors": ["#abc"]})
    assert r.status_code == 200
    assert r.json()["colors"] == ["#abc"]


def test_delete_profile_simple(client, seeded_profile):
    r = client.delete(f"/api/profiles/{seeded_profile['id']}")
    assert r.status_code == 204
    assert client.get(f"/api/profiles/{seeded_profile['id']}").status_code == 404


def test_delete_profile_404(client):
    assert client.delete("/api/profiles/9999").status_code == 404


def test_delete_with_spools_409(client, seeded_profile):
    client.post("/api/spools", json={"profile_id": seeded_profile["id"]})
    r = client.delete(f"/api/profiles/{seeded_profile['id']}")
    assert r.status_code == 409


def test_delete_with_spools_force_cascades(client, seeded_profile):
    client.post("/api/spools", json={"profile_id": seeded_profile["id"]})
    r = client.delete(f"/api/profiles/{seeded_profile['id']}?force=true")
    assert r.status_code == 204
    assert client.get(f"/api/profiles/{seeded_profile['id']}").status_code == 404
    assert client.get("/api/spools").json() == []


# ---------------------------------------------------------------------------
# Delete with printer_db_id — typed-token confirm (Rule 5/7)
# ---------------------------------------------------------------------------

def test_delete_with_printer_entry_needs_token(client, profile_with_printer):
    pid = profile_with_printer["id"]
    assert client.delete(f"/api/profiles/{pid}").status_code == 409
    assert client.delete(f"/api/profiles/{pid}?confirm=wrong").status_code == 409
    # URL-encoded "+" → %2B; Starlette decodes the query param to "Sunlu PLA+"
    assert client.delete(f"/api/profiles/{pid}?confirm=Sunlu PLA%2B").status_code == 204


def test_delete_with_printer_entry_force_alone_still_409(client, profile_with_printer):
    """force=true does not bypass the typed-token requirement."""
    pid = profile_with_printer["id"]
    assert client.delete(f"/api/profiles/{pid}?force=true").status_code == 409


def test_delete_with_printer_and_spools_needs_both(client, profile_with_printer):
    """printer_db_id + spool reference: need confirm AND force."""
    pid = profile_with_printer["id"]
    client.post("/api/spools", json={"profile_id": pid})
    # confirm alone → 409 (spools block)
    assert client.delete(f"/api/profiles/{pid}?confirm=Sunlu PLA%2B").status_code == 409
    # force alone → 409 (printer needs confirm)
    assert client.delete(f"/api/profiles/{pid}?force=true").status_code == 409
    # both → 204
    r = client.delete(f"/api/profiles/{pid}?force=true&confirm=Sunlu PLA%2B")
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# Validate endpoint
# ---------------------------------------------------------------------------

def test_validate_returns_warnings(client):
    """A profile whose name lacks the vendor should warn."""
    p = _make_profile(client, brand="Sunlu", name="PLA+", type="PLA")
    r = client.post(f"/api/profiles/{p['id']}/validate")
    assert r.status_code == 200
    warnings = r.json()["warnings"]
    assert any("vendor" in w.lower() for w in warnings)


def test_validate_no_warnings_for_good_profile(client, seeded_profile):
    r = client.post(f"/api/profiles/{seeded_profile['id']}/validate")
    assert r.status_code == 200
    assert r.json()["warnings"] == []


def test_validate_unknown_type_warns(client):
    p = _make_profile(client, brand="Sunlu", name="Sunlu Weird", type="WEIRD")
    r = client.post(f"/api/profiles/{p['id']}/validate")
    assert r.status_code == 200
    assert any("unknown type" in w.lower() for w in r.json()["warnings"])


def test_validate_404(client):
    assert client.post("/api/profiles/9999/validate").status_code == 404


# ---------------------------------------------------------------------------
# Spool CRUD
# ---------------------------------------------------------------------------

def test_create_spool(client, seeded_profile):
    r = client.post("/api/spools", json={
        "profile_id": seeded_profile["id"], "remaining_pct": 80, "location": "shelf A",
    })
    assert r.status_code == 201
    assert r.json()["remaining_pct"] == 80
    assert r.json()["location"] == "shelf A"


def test_create_spool_404_profile(client):
    assert client.post("/api/spools", json={"profile_id": 9999}).status_code == 404


def test_list_spools(client, seeded_profile):
    client.post("/api/spools", json={"profile_id": seeded_profile["id"]})
    r = client.get("/api/spools")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_patch_spool(client, seeded_profile):
    r = client.post("/api/spools", json={"profile_id": seeded_profile["id"]})
    sid = r.json()["id"]
    r = client.patch(f"/api/spools/{sid}", json={"remaining_pct": 50})
    assert r.status_code == 200
    assert r.json()["remaining_pct"] == 50


def test_patch_spool_404(client):
    assert client.patch("/api/spools/9999", json={"remaining_pct": 50}).status_code == 404


def test_patch_spool_reassign_profile(client, seeded_profile):
    other = _make_profile(client, brand="eSUN", name="eSUN PETG Basic", type="PETG")
    r = client.post("/api/spools", json={"profile_id": seeded_profile["id"]})
    sid = r.json()["id"]
    r = client.patch(f"/api/spools/{sid}", json={"profile_id": other["id"]})
    assert r.status_code == 200
    assert r.json()["profile_id"] == other["id"]


def test_patch_spool_reassign_to_missing_profile_404(client, seeded_profile):
    r = client.post("/api/spools", json={"profile_id": seeded_profile["id"]})
    sid = r.json()["id"]
    assert client.patch(f"/api/spools/{sid}", json={"profile_id": 9999}).status_code == 404


def test_delete_spool(client, seeded_profile):
    r = client.post("/api/spools", json={"profile_id": seeded_profile["id"]})
    sid = r.json()["id"]
    assert client.delete(f"/api/spools/{sid}").status_code == 204
    assert client.get("/api/spools").json() == []


def test_delete_spool_404(client):
    assert client.delete("/api/spools/9999").status_code == 404
