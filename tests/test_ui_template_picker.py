# tests/test_ui_template_picker.py
"""Tests for the template picker HTMX fragment endpoint."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fm.api.app import create_app
from fm.db import SessionLocal
from fm.models import Profile


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/picker.db")
    with TestClient(create_app()) as c:
        yield c


def test_template_dropdown_new_profile(client):
    """Fragment for new profile with type=PETG returns vendor + template selects."""
    r = client.get("/profiles/new/template-dropdown?type=PETG")
    assert r.status_code == 200
    assert b'template-vendor' in r.content
    assert b'template-override' in r.content
    assert b'All vendors' in r.content
    assert b'Use Generic PETG defaults' in r.content
    assert b'Bambu PETG Translucent' in r.content


def test_template_dropdown_filtered_by_vendor(client):
    """Fragment with vendor filter narrows template list."""
    r = client.get("/profiles/new/template-dropdown?type=PETG&template_vendor=Bambu+Lab")
    assert r.status_code == 200
    assert b'Bambu PETG Translucent' in r.content
    # Non-Bambu templates should not appear in optgroups
    assert b'Creality Generic PETG' not in r.content


def test_template_dropdown_type_change_resets(client):
    """Type change resets vendor and template to generic default."""
    r = client.get("/profiles/new/template-dropdown?type=PLA&reason=type-change")
    assert r.status_code == 200
    assert b'Use Generic PLA defaults' in r.content
    assert b'reset' in r.content.lower() or b'Generic PLA' in r.content


def test_template_dropdown_with_preview(client):
    """Selecting a template shows preview values."""
    r = client.get("/profiles/new/template-dropdown?type=PETG&template_override=Bambu+PETG+Translucent")
    assert r.status_code == 200
    assert b'template-preview' in r.content
    assert b'0.95' in r.content  # flow ratio


def test_template_dropdown_edit_profile(client):
    """Fragment for existing profile shows saved template_override selected."""
    # Create a profile via API
    r = client.post("/api/profiles", json={
        "brand": "Bambu Lab", "name": "PETG Translucent", "type": "PETG",
        "min_temp": 230, "max_temp": 270, "density": 1.25,
        "template_override": "Bambu PETG Translucent",
    })
    assert r.status_code == 201
    pid = r.json()["id"]

    r = client.get(f"/profiles/{pid}/template-dropdown?type=PETG&template_override=Bambu+PETG+Translucent")
    assert r.status_code == 200
    assert b'selected' in r.content
    assert b'Bambu PETG Translucent' in r.content


def test_profile_form_has_template_picker(client):
    """Profile form page includes the template picker container."""
    r = client.get("/profiles/new")
    assert r.status_code == 200
    assert b'template-picker' in r.content


def test_profile_form_submit_with_template(client):
    """Submitting the form with template_override persists it."""
    r = client.post("/profiles/form", data={
        "brand": "Bambu Lab", "name": "PETG Translucent", "type": "PETG",
        "min_temp": "230", "max_temp": "270", "density": "1.25",
        "drying_temp": "65", "drying_time": "8",
        "template_override": "Bambu PETG Translucent",
    }, follow_redirects=False)
    assert r.status_code == 303
    # Verify it persisted
    from fm.db import SessionLocal
    from fm.models import Profile
    with SessionLocal() as s:
        p = s.scalars(select(Profile).order_by(Profile.id.desc())).first()
        assert p.template_override == "Bambu PETG Translucent"


def test_profile_form_submit_without_template(client):
    """Submitting without template_override sets it to None."""
    r = client.post("/profiles/form", data={
        "brand": "Sunlu", "name": "PLA+", "type": "PLA",
        "min_temp": "200", "max_temp": "240", "density": "1.24",
        "drying_temp": "60", "drying_time": "8",
        "template_override": "",
    }, follow_redirects=False)
    assert r.status_code == 303
    from fm.db import SessionLocal
    from fm.models import Profile
    with SessionLocal() as s:
        p = s.scalars(select(Profile).order_by(Profile.id.desc())).first()
        assert p.template_override is None


def test_type_mismatch_clears_template_override(client):
    """Submitting type=PLA with a PETG template_override clears the override."""
    r = client.post("/profiles/form", data={
        "brand": "Bambu Lab", "name": "PETG Translucent", "type": "PLA",
        "min_temp": "200", "max_temp": "240", "density": "1.24",
        "drying_temp": "60", "drying_time": "8",
        "template_override": "Bambu PETG Translucent",  # PETG template with PLA type
    }, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        p = s.scalars(select(Profile).order_by(Profile.id.desc())).first()
        assert p.template_override is None  # cleared due to type mismatch
