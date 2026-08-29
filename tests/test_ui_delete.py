# tests/test_ui_delete.py
"""Profile delete UI — confirmation page and form round-trip.

Mirrors the API delete semantics (spec §6 + Iron Rule 5/7) at the page-route
level: typed-token confirm for pushed profiles, spool cascade checkbox,
sync-target cleanup, opt-in Orca Cloud preset deletion.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from unittest.mock import MagicMock

from fm.db import SessionLocal
from fm.models import ActivityLog, Profile, SyncTarget
from fm.orca_cloud import OrcaCloudError
from fm.service import sync_handlers as sh


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/ui-del.db")
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


def _add_spool(client, pid: int) -> None:
    r = client.post("/api/spools", json={"profile_id": pid, "remaining_pct": 80})
    assert r.status_code == 201, r.text


def _push_profile(pid: int, db_id: str = "99001") -> None:
    with SessionLocal() as s:
        row = s.get(Profile, pid)
        row.printer_db_id = db_id
        s.add(SyncTarget(profile_id=pid, kind="printer_db", status="in_sync"))
        s.add(SyncTarget(profile_id=pid, kind="orca_preset", cloud_id="abc-123"))


@pytest.fixture
def fake_orca(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(sh, "_orca_client_factory", lambda: client)
    return client


# ---------------------------------------------------------------------------
# Confirmation page
# ---------------------------------------------------------------------------

def test_delete_page_renders(client):
    p = _make_profile(client)
    r = client.get(f"/profiles/{p['id']}/delete")
    assert r.status_code == 200
    assert b"Delete profile" in r.content


def test_delete_page_404(client):
    assert client.get("/profiles/9999/delete").status_code == 404


def test_delete_page_shows_confirm_input_for_pushed(client):
    p = _make_profile(client)
    _push_profile(p["id"])
    r = client.get(f"/profiles/{p['id']}/delete")
    assert r.status_code == 200
    assert b"to confirm" in r.content
    assert b"99001" in r.content
    assert b"tick the checkbox below" in r.content
    assert b'name="delete_cloud"' in r.content


def test_delete_page_shows_spool_checkbox(client):
    p = _make_profile(client)
    _add_spool(client, p["id"])
    r = client.get(f"/profiles/{p['id']}/delete")
    assert r.status_code == 200
    assert b'name="force"' in r.content


# ---------------------------------------------------------------------------
# Form round-trip
# ---------------------------------------------------------------------------

def test_delete_unpushed_profile_redirects(client):
    p = _make_profile(client)
    r = client.post(f"/profiles/{p['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/profiles?deleted=1"
    assert client.get(f"/api/profiles/{p['id']}").status_code == 404


def test_delete_pushed_profile_requires_typed_token(client):
    p = _make_profile(client)
    _push_profile(p["id"])
    r = client.post(f"/profiles/{p['id']}/delete", data={"confirm": "wrong"})
    assert r.status_code == 200
    assert b"must match the profile name" in r.content
    # unchanged
    assert client.get(f"/api/profiles/{p['id']}").status_code == 200


def test_delete_pushed_profile_with_token_removes_sync_targets(client):
    p = _make_profile(client, name="Sunlu PLA+")
    _push_profile(p["id"])
    r = client.post(
        f"/profiles/{p['id']}/delete",
        data={"confirm": "Sunlu PLA+"}, follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get(f"/api/profiles/{p['id']}").status_code == 404
    with SessionLocal() as s:
        assert s.query(SyncTarget).filter_by(profile_id=p["id"]).count() == 0


def test_delete_with_spools_requires_force(client):
    p = _make_profile(client)
    _add_spool(client, p["id"])
    r = client.post(f"/profiles/{p['id']}/delete")
    assert r.status_code == 200
    assert b"spool(s) in stock" in r.content
    assert client.get(f"/api/profiles/{p['id']}").status_code == 200


def test_delete_with_spools_force_cascades(client):
    p = _make_profile(client)
    _add_spool(client, p["id"])
    _add_spool(client, p["id"])
    r = client.post(
        f"/profiles/{p['id']}/delete", data={"force": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get(f"/api/profiles/{p['id']}").status_code == 404
    assert client.get("/api/spools").json() == []


def test_delete_button_listed_on_profiles_page(client):
    p = _make_profile(client)
    r = client.get("/profiles")
    assert r.status_code == 200
    assert f'/profiles/{p["id"]}/delete'.encode() in r.content


# ---------------------------------------------------------------------------
# Opt-in Orca Cloud preset deletion
# ---------------------------------------------------------------------------

def test_delete_without_cloud_checkbox_keeps_preset(client, fake_orca):
    p = _make_profile(client, name="Sunlu PLA+")
    _push_profile(p["id"])
    r = client.post(
        f"/profiles/{p['id']}/delete",
        data={"confirm": "Sunlu PLA+"}, follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/profiles?deleted=1"
    assert client.get(f"/api/profiles/{p['id']}").status_code == 404
    fake_orca.sync_delete.assert_not_called()


def test_delete_with_cloud_checkbox_calls_sync_delete(client, fake_orca):
    p = _make_profile(client, name="Sunlu PLA+")
    _push_profile(p["id"])
    r = client.post(
        f"/profiles/{p['id']}/delete",
        data={"confirm": "Sunlu PLA+", "delete_cloud": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/profiles?deleted=1"
    assert client.get(f"/api/profiles/{p['id']}").status_code == 404
    fake_orca.sync_delete.assert_called_once_with(["abc-123"])


def test_delete_cloud_unconfigured_fails_soft(client, monkeypatch):
    monkeypatch.setattr(sh, "_orca_client_factory", lambda: None)
    p = _make_profile(client, name="Sunlu PLA+")
    _push_profile(p["id"])
    r = client.post(
        f"/profiles/{p['id']}/delete",
        data={"confirm": "Sunlu PLA+", "delete_cloud": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/profiles?deleted=1&cloud_failed=1"
    assert client.get(f"/api/profiles/{p['id']}").status_code == 404


def test_delete_cloud_error_fails_soft(client, fake_orca):
    fake_orca.sync_delete.side_effect = OrcaCloudError(500, "boom")
    p = _make_profile(client, name="Sunlu PLA+")
    _push_profile(p["id"])
    r = client.post(
        f"/profiles/{p['id']}/delete",
        data={"confirm": "Sunlu PLA+", "delete_cloud": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/profiles?deleted=1&cloud_failed=1"
    assert client.get(f"/api/profiles/{p['id']}").status_code == 404
    with SessionLocal() as s:
        row = s.query(ActivityLog).filter_by(action="orca_cloud_delete").one()
        assert row.outcome == "warn"


def test_delete_cloud_checkbox_no_target_skips_cloud(client, fake_orca):
    p = _make_profile(client)
    r = client.post(
        f"/profiles/{p['id']}/delete",
        data={"delete_cloud": "1"}, follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/profiles?deleted=1"
    assert client.get(f"/api/profiles/{p['id']}").status_code == 404
    fake_orca.sync_delete.assert_not_called()
