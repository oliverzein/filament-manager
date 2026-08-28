# tests/test_api_templates.py
"""Tests for the /api/templates endpoint."""
import pytest
from fastapi.testclient import TestClient

from fm.api.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    with TestClient(create_app()) as c:
        yield c


def test_list_templates_no_filter(client):
    r = client.get("/api/templates")
    assert r.status_code == 200
    data = r.json()
    assert "templates" in data
    assert len(data["templates"]) > 300


def test_list_templates_filtered_by_type(client):
    r = client.get("/api/templates?type=PETG")
    assert r.status_code == 200
    data = r.json()
    assert all(t["type"] == "PETG" for t in data["templates"])
    assert "Bambu PETG Translucent" in [t["name"] for t in data["templates"]]


def test_list_templates_filtered_by_type_and_vendor(client):
    r = client.get("/api/templates?type=PETG&vendor=Bambu")
    assert r.status_code == 200
    data = r.json()
    assert all("bambu" in t["vendor"].lower() for t in data["templates"])
    assert "vendors" in data


def test_list_templates_vendors_for_type(client):
    r = client.get("/api/templates?type=PLA")
    assert r.status_code == 200
    data = r.json()
    assert "Generic" in data["vendors"]
    assert data["vendors"][0] == "Generic"
