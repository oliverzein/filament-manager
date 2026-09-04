from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from fm.api.app import create_app, Settings
from fm.db import init_engine
from fm.models import Base, ProfileCache, Spool
from sqlalchemy.orm import Session


@pytest.fixture
def client(tmp_path):
    db_url = f"sqlite:///{tmp_path}/test_spools.db"
    engine = init_engine(db_url)
    with Session(engine) as s:
        p1 = ProfileCache(
            cloud_resource_id="uuid-active",
            name="Sunlu PLA+",
            vendor="Sunlu",
            type="PLA",
            deleted_from_cloud=False,
            last_seen_at=datetime.now(timezone.utc),
        )
        p2 = ProfileCache(
            cloud_resource_id="uuid-orphan",
            name="Old PETG",
            vendor="eSUN",
            type="PETG",
            deleted_from_cloud=True,
            deleted_at=datetime.now(timezone.utc),
            last_seen_at=datetime.now(timezone.utc),
        )
        s.add_all([p1, p2])
        s.flush()

        s1 = Spool(cloud_resource_id="uuid-active", remaining_pct=80, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        s2 = Spool(cloud_resource_id="uuid-orphan", remaining_pct=20, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        s.add_all([s1, s2])
        s.commit()

    app = create_app(Settings(database_url=db_url))
    return TestClient(app)


def test_spools_list_shows_profiles_and_orphan_badge(client):
    resp = client.get("/spools")
    assert resp.status_code == 200
    html = resp.text
    assert "Sunlu PLA+" in html
    assert "Old PETG" in html
    assert "Orphan" in html or "orphan" in html


def test_add_spool_with_cloud_resource_id(client):
    resp = client.post("/spools/form", data={
        "cloud_resource_id": "uuid-active",
        "remaining_pct": "100",
        "location": "Shelf A",
    }, follow_redirects=False)
    assert resp.status_code == 303
